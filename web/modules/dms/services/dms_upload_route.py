#!/usr/bin/env python3
"""
Upload endpoint for the React matter workspace.
POST /dms/disk/upload  (multipart: file, disk_path, matter_id)
POST /dms/disk/version-link  (JSON: new_doc_id, parent_doc_id)
GET  /dms/disk/version-chain/{doc_id}

Email endpoints:
POST /dms/disk/email-file   — Postfix path (system/external emails, attachments)
POST /dms/disk/send-email   — Connector path (EWS for internal users, SMTP fallback)
POST /dms/disk/stage-attachment — Desktop drag-and-drop staging

Writes to the matter's Praesidium disk root.
On upload: computes SHA-256, inserts documents row, runs similarity
check against existing matter documents, returns match candidates.
"""
import hashlib
import mimetypes
import os
import logging
import json
import uuid as _uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import JSONResponse
from typing import Optional
from pydantic import BaseModel
from sqlalchemy import text as sa_text

from core.db.base import AsyncSessionLocal

# Email with attachment support (for email-file Postfix path)
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/dms", tags=["dms-upload"])

PRAESIDIUM_ROOT = "/mnt/praesidium"


def _tid(r):
    return (getattr(r.state, "tenant_id", "") or "").strip()


def _user_id(r):
    """Extract user ID from request state (bigint or uuid depending on context)."""
    user = getattr(r.state, "current_user", None)
    if user is None:
        return None
    if hasattr(user, "id"):
        return user.id
    return user


def _uid_uuid(r):
    """Return the current user id only if it is UUID-shaped, else None.
    audit_log.user_id is uuid; users.id may be bigint in some contexts."""
    uid = _user_id(r)
    if uid is None:
        return None
    try:
        return str(_uuid.UUID(str(uid)))
    except (ValueError, AttributeError, TypeError):
        return None

def _actor(r):
    """actor_name / actor_user_id for audit details (best-effort, integer-id safe)."""
    u = getattr(r.state, "current_user", None)
    out = {}
    if u is not None:
        uid = getattr(u, "id", None)
        if uid is not None:
            out["actor_user_id"] = str(uid)
        nm = getattr(u, "full_name", None) or getattr(u, "name", None)
        if nm:
            out["actor_name"] = nm
    return out



async def _write_audit(tid, action, entity_id, *, user_uuid=None,
                       entity_type="document", table_name="documents",
                       old=None, new=None, details=None):
    """Append-only audit_log row. Never fatal -- logs and swallows on error.
    asyncpg-safe: no parameterized NULL casts. user_id is omitted (always null
    in practice; the acting user rides in details). jsonb columns always get a
    real JSON string ('null' for None)."""
    try:
        if not entity_id:
            return
        det = dict(details) if isinstance(details, dict) else ({} if details is None else {"note": details})
        if user_uuid is not None:
            det.setdefault("user_uuid", str(user_uuid))
        async with AsyncSessionLocal() as session:
            await session.execute(sa_text("""
                INSERT INTO audit_log
                    (id, tenant_id, action, entity_type, entity_id,
                     table_name, record_id, old_values, new_values, details, created_at)
                VALUES
                    (gen_random_uuid(), :tid, :action, :etype, CAST(:eid AS uuid),
                     :tbl, :eid, CAST(:old AS jsonb), CAST(:new AS jsonb),
                     CAST(:det AS jsonb), NOW())
            """), {
                "tid": tid, "action": action, "etype": entity_type,
                "eid": str(entity_id), "tbl": table_name,
                "old": json.dumps(old), "new": json.dumps(new), "det": json.dumps(det),
            })
            await session.commit()
    except Exception as e:
        logger.warning("audit_log write failed (non-fatal): %s", e)


async def _stamp_provenance(tid, doc_id, provenance):
    """Merge a provenance object into documents.metadata. Non-fatal."""
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(sa_text("""
                UPDATE documents
                SET metadata = COALESCE(metadata, '{}'::jsonb)
                              || jsonb_build_object('provenance', CAST(:prov AS jsonb)),
                    updated_at = NOW()
                WHERE id = CAST(:did AS uuid) AND TRIM(tenant_id) = :tid
            """), {"prov": json.dumps(provenance), "did": str(doc_id), "tid": tid})
            await session.commit()
    except Exception as e:
        logger.warning("provenance stamp failed (non-fatal): %s", e)


async def _resolve_matter_root(tid, matter_id):
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT m.matter_name, c.client_name
            FROM matters m
            LEFT JOIN clients c ON m.client_id = c.id
                AND trim(m.tenant_id) = trim(c.tenant_id)
            WHERE m.id = CAST(:mid AS uuid)
              AND trim(m.tenant_id) = trim(:tid)
        """), {"mid": matter_id, "tid": tid})
        row = r.mappings().fetchone()
    if not row:
        return None
    client = row["client_name"]
    matter = row["matter_name"]
    if not client or not matter:
        return None
    p = os.path.join(PRAESIDIUM_ROOT, tid, "matters", client, matter)
    if not os.path.isdir(p):
        os.makedirs(p, exist_ok=True)
    return p


@router.post("/disk/upload")
async def disk_upload(
    request: Request,
    file: UploadFile = File(...),
    disk_path: str = Form(""),
    matter_id: str = Form(...),
):
    tid = _tid(request)
    if not tid:
        raise HTTPException(status_code=403, detail="No tenant resolved")

    root = await _resolve_matter_root(tid, matter_id)
    if not root:
        raise HTTPException(status_code=404, detail="Matter disk root not found")

    # Resolve target directory
    if disk_path and disk_path != ".":
        target_dir = os.path.join(root, disk_path)
    else:
        target_dir = root

    # Path traversal check
    resolved_dir = os.path.realpath(target_dir)
    if not resolved_dir.startswith(os.path.realpath(root)):
        raise HTTPException(status_code=403, detail="Path traversal denied")

    os.makedirs(resolved_dir, exist_ok=True)

    # Read file content
    filename = os.path.basename(file.filename or "upload")
    content = await file.read()
    file_size = len(content)

    # Compute SHA-256 checksum
    checksum = hashlib.sha256(content).hexdigest()

    # ── Version/duplicate check ──────────────────────────────────────
    version_matches = []
    is_exact_duplicate = False
    duplicate_doc = None

    try:
        from modules.dms.services.dms_version_service import (
            check_for_versions,
            VersionCheckResult,
        )

        vcheck: VersionCheckResult = await check_for_versions(
            tenant_id=tid,
            matter_id=matter_id,
            content=content,
            filename=filename,
            target_folder=disk_path,
        )

        if vcheck.is_exact_duplicate and vcheck.duplicate_of:
            is_exact_duplicate = True
            duplicate_doc = {
                "document_id": vcheck.duplicate_of.document_id,
                "filename": vcheck.duplicate_of.filename,
                "similarity": 1.0,
                "match_type": "exact_duplicate",
            }

        for match in vcheck.similar_documents:
            version_matches.append({
                "document_id": match.document_id,
                "filename": match.filename,
                "similarity": match.similarity,
                "match_type": match.match_type,
                "version_number": match.version_number,
                "created_at": match.created_at,
            })

        extracted_text = vcheck.extracted_text

    except ImportError:
        logger.warning("dms_version_service not available — skipping version check")
        extracted_text = ""
    except Exception as e:
        logger.error("Version check failed (non-fatal): %s", e)
        extracted_text = ""

    # ── Write file to disk ───────────────────────────────────────────
    dest = os.path.join(resolved_dir, filename)

    # Auto-rename if exists on disk
    actual_filename = filename
    if os.path.exists(dest):
        base, ext = os.path.splitext(filename)
        i = 1
        while os.path.exists(dest):
            actual_filename = f"{base} ({i}){ext}"
            dest = os.path.join(resolved_dir, actual_filename)
            i += 1

    try:
        with open(dest, "wb") as f:
            f.write(content)
        logger.info("Uploaded %s to %s (%d bytes, sha256=%s)",
                     actual_filename, dest, file_size, checksum[:16])
    except Exception as e:
        logger.error("Upload write failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Upload failed: {e}")

    # ── Insert documents row ─────────────────────────────────────────
    doc_id = str(_uuid.uuid4())
    mime_type = mimetypes.guess_type(actual_filename)[0] or "application/octet-stream"
    user_id = _user_id(request)
    rel_path = os.path.relpath(dest, PRAESIDIUM_ROOT)  # relative to mount root

    try:
        async with AsyncSessionLocal() as session:
            await session.execute(
                sa_text("""
                    INSERT INTO documents (
                        id, tenant_id, matter_id, filename, original_filename,
                        mime_type, file_size, storage_path, checksum,
                        extracted_text, version_number, status, created_by,
                        created_at, updated_at
                    ) VALUES (
                        CAST(:id AS uuid), :tid, CAST(:mid AS uuid),
                        :fname, :orig_fname,
                        :mime, :fsize, :spath, :cs,
                        :etext, 1, 'active', :uid,
                        NOW(), NOW()
                    )
                    ON CONFLICT DO NOTHING
                """),
                {
                    "id": doc_id,
                    "tid": tid,
                    "mid": matter_id,
                    "fname": actual_filename,
                    "orig_fname": filename,
                    "mime": mime_type,
                    "fsize": file_size,
                    "spath": dest,
                    "cs": checksum,
                    "etext": extracted_text if extracted_text else None,
                    "uid": user_id,
                },
            )
            await session.commit()
    except Exception as e:
        logger.error("Document row insert failed (file saved): %s", e)

    # ── Register in dms_documents + trigger extraction ───────────────
    dms_doc_id = str(_uuid.uuid4())
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(
                sa_text("""
                    INSERT INTO dms_documents
                        (id, tenant_id, file_path, folder_root, file_hash,
                         file_size_bytes, extraction_status, source, updated_at)
                    VALUES
                        (CAST(:did AS uuid), :tid, :fpath, :froot, :fhash,
                         :fsize, :ext_status, 'upload', NOW())
                    ON CONFLICT DO NOTHING
                """),
                {
                    "did": dms_doc_id,
                    "tid": tid,
                    "fpath": dest,
                    "froot": root,
                    "fhash": checksum,
                    "fsize": file_size,
                    "ext_status": "complete" if extracted_text else "pending",
                },
            )
            if extracted_text:
                await session.execute(
                    sa_text("UPDATE dms_documents SET content_text = :txt WHERE id = CAST(:did AS uuid)"),
                    {"txt": extracted_text[:200000], "did": dms_doc_id},
                )
            await session.commit()
    except Exception as e:
        logger.error("dms_documents insert failed (non-fatal): %s", e)

    # ── Extraction (OCR for scans) + classification ("selector") ─────────────
    # Split on OCR: printed / converted-Word PDFs (pleadings, exhibit lists,
    # briefs) arrive with a text layer -> classify now, no OCR. Scanned docs
    # (exhibits) have no text layer -> run the extract job (OCRs via tesseract),
    # then classify once text exists. classify_and_route auto-ingests exhibit
    # lists into the Trial Center.
    try:
        from redis import Redis
        from rq import Queue
        redis_url = os.environ.get("REDIS_URL", "redis://praesidium-redis:6379/0")
        _rparts = redis_url.replace("redis://", "").split("/")
        _rhp = _rparts[0].split(":")
        _rconn = Redis(host=_rhp[0], port=int(_rhp[1]) if len(_rhp) > 1 else 6379,
                       db=int(_rparts[1]) if len(_rparts) > 1 and _rparts[1] else 0)
        _q = Queue("default", connection=_rconn)
        _CLASSIFY = "modules.intelligence.document_legal_classifier.classify_and_route_one_sync"
        if extracted_text:
            # text-native -> classify immediately (no OCR needed)
            _q.enqueue(_CLASSIFY, tid, doc_id, job_timeout=600)
        else:
            # scanned -> OCR via the extract job, then classify on completion
            _job = _q.enqueue(
                "jobs.dms_extract_job.run_extract_single",
                tid, dest, dms_doc_id,
                job_timeout=300, result_ttl=3600,
            )
            logger.info("Enqueued single-file extraction (OCR) for %s", dest)
            _q.enqueue(_CLASSIFY, tid, doc_id, depends_on=_job, job_timeout=600)
    except Exception as e:
        logger.warning("Failed to enqueue extraction/classify (non-fatal): %s", e)

    # ── Build response ───────────────────────────────────────────────
    # provenance + audit (non-fatal)
    await _stamp_provenance(tid, doc_id, {
        "origin": "upload",
        "created_via": "disk_upload",
        "folder": disk_path or "",
        "actor": str(user_id) if user_id is not None else None,
        "at": datetime.now(timezone.utc).isoformat(),
    })
    await _write_audit(tid, "document.uploaded", doc_id,
        user_uuid=_uid_uuid(request),
        new={"filename": actual_filename, "file_size": file_size,
             "checksum": checksum, "version_number": 1},
        details={"folder": disk_path or "", "original_filename": filename, **_actor(request)})

    response = {
        "status": "ok",
        "document_id": doc_id,
        "filename": actual_filename,
        "original_filename": filename,
        "path": os.path.relpath(dest, root),
        "absolute_path": dest,
        "size": file_size,
        "checksum": checksum,
    }

    if is_exact_duplicate and duplicate_doc:
        response["warning"] = "exact_duplicate"
        response["duplicate_of"] = duplicate_doc
        response["message"] = (
            f"This file is identical to existing document "
            f"'{duplicate_doc['filename']}'. File saved but may be a duplicate."
        )

    if version_matches:
        response["version_candidates"] = version_matches
        best = version_matches[0]
        response["best_match"] = {
            "document_id": best["document_id"],
            "filename": best["filename"],
            "similarity": best["similarity"],
            "similarity_pct": f"{best['similarity'] * 100:.0f}%",
        }
        response["message"] = (
            f"This file is {best['similarity'] * 100:.0f}% similar to "
            f"'{best['filename']}'. Link as a new version?"
        )

    return JSONResponse(response)



# ══════════════════════════════════════════════════════════════════════
# Email file endpoint — POSTFIX PATH (system/external emails)
# ══════════════════════════════════════════════════════════════════════
# This endpoint sends via Postfix SMTP relay directly.  It is the path
# for system-generated emails, external/third-party correspondence, and
# any future client-facing email features.  NOT for internal user-
# initiated compose/reply/forward — those use /disk/send-email below.
# ══════════════════════════════════════════════════════════════════════

class EmailFileRequest(BaseModel):
    to: str
    cc: Optional[str] = None
    subject: str = ""
    body_text: str = ""
    file_path: Optional[str] = None       # single file (backward compat)
    filename: Optional[str] = None        # single file (backward compat)
    files: Optional[list] = None          # list of {file_path, filename}
    matter_id: str


@router.post("/disk/email-file")
async def email_file(request: Request, body: EmailFileRequest):
    """Send email with one or more DMS files as attachments via Postfix.
    Sends FROM the logged-in user's email. Files a copy to matter's Email folder.
    This is the POSTFIX path — for system/external emails."""
    tid = _tid(request)
    if not tid:
        raise HTTPException(status_code=403, detail="No tenant resolved")

    root = await _resolve_matter_root(tid, body.matter_id)
    if not root:
        raise HTTPException(status_code=404, detail="Matter disk root not found")

    # Build file list (support both single file and multi-file)
    file_list = []
    if body.files:
        file_list = [{"file_path": f.get("file_path",""), "filename": f.get("filename","")} for f in body.files]
    elif body.file_path and body.filename:
        file_list = [{"file_path": body.file_path, "filename": body.filename}]

    if not file_list:
        raise HTTPException(status_code=400, detail="No files specified")

    # Validate all file paths
    resolved_files = []
    for fi in file_list:
        fp = os.path.join(root, fi["file_path"])
        rp = os.path.realpath(fp)
        if not rp.startswith(os.path.realpath(root)):
            raise HTTPException(status_code=403, detail=f"Path traversal denied: {fi['filename']}")
        if not os.path.isfile(rp):
            raise HTTPException(status_code=404, detail=f"File not found: {fi['filename']}")
        resolved_files.append({"resolved": rp, "filename": fi["filename"]})

    # Get logged-in user
    user = getattr(request.state, "current_user", None)
    user_email = getattr(user, "email", None) if user else None
    user_name = getattr(user, "full_name", None) if user else None

    try:
        from core.services.email_service import _load_smtp_config, _check_postfix_container
        cfg = await _load_smtp_config(tid)

        from_addr = user_email or cfg["from_addr"]
        from_name = user_name or cfg["from_name"]

        msg = MIMEMultipart("mixed")
        msg["From"] = f"{from_name} <{from_addr}>"
        msg["To"] = body.to
        msg["Subject"] = body.subject or resolved_files[0]["filename"]
        msg["Date"] = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
        msg["Message-ID"] = f"<{_uuid.uuid4()}@{from_addr.split(chr(64))[-1] if chr(64) in from_addr else 'praesidium'}>"
        if body.cc:
            msg["Cc"] = body.cc

        # Body
        msg.attach(MIMEText(body.body_text or "", "plain", "utf-8"))

        # Attach all files
        for fi in resolved_files:
            mt = mimetypes.guess_type(fi["filename"])[0] or "application/octet-stream"
            main_t, sub_t = mt.split("/", 1)
            with open(fi["resolved"], "rb") as af:
                att = MIMEBase(main_t, sub_t)
                att.set_payload(af.read())
            encoders.encode_base64(att)
            att.add_header("Content-Disposition", "attachment", filename=fi["filename"])
            msg.attach(att)

        # Send via Postfix
        recipients = [body.to]
        if body.cc:
            recipients.extend([a.strip() for a in body.cc.split(",") if a.strip()])

        if _check_postfix_container():
            smtp_host, smtp_port = "praesidium-smtp", 25
        else:
            smtp_host, smtp_port = cfg["host"], cfg["port"]

        with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
            if cfg.get("use_tls"):
                server.starttls()
            if cfg.get("username") and cfg.get("password"):
                server.login(cfg["username"], cfg["password"])
            server.sendmail(from_addr, recipients, msg.as_string())

        logger.info("Email sent from %s to %s with %d attachment(s)", from_addr, body.to, len(resolved_files))

        # File to matter
        eml_filename = None
        if root:
            try:
                email_folder = None
                for candidate in ["15-Email", "09-Email", "Email"]:
                    cp = os.path.join(root, candidate)
                    if os.path.isdir(cp):
                        email_folder = cp
                        break
                if not email_folder:
                    email_folder = os.path.join(root, "15-Email")
                    os.makedirs(email_folder, exist_ok=True)

                ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                safe_subj = "".join(c for c in (body.subject or "sent")[:60] if c.isalnum() or c in " -_").strip()
                eml_filename = f"{ts}_{safe_subj}.eml"
                eml_path = os.path.join(email_folder, eml_filename)
                with open(eml_path, "w") as ef:
                    ef.write(msg.as_string())

                doc_id = str(_uuid.uuid4())
                eml_cs = hashlib.sha256(msg.as_string().encode()).hexdigest()
                eml_sz = os.path.getsize(eml_path)
                async with AsyncSessionLocal() as session:
                    await session.execute(sa_text("""
                        INSERT INTO documents (id, tenant_id, matter_id, filename, original_filename,
                            mime_type, file_size, storage_path, checksum, version_number, status, created_at, updated_at)
                        VALUES (CAST(:id AS uuid), :tid, CAST(:mid AS uuid), :fname, :fname,
                            'message/rfc822', :fsize, :spath, :cs, 1, 'active', NOW(), NOW())
                        ON CONFLICT DO NOTHING
                    """), {"id":doc_id,"tid":tid,"mid":body.matter_id,"fname":eml_filename,"fsize":eml_sz,"spath":eml_path,"cs":eml_cs})
                    await session.commit()
            except Exception as e:
                logger.warning("Email sent but filing failed: %s", e)

        return JSONResponse({
            "status": "ok", "message": f"Email sent to {body.to}",
            "from": from_addr, "attachments": len(resolved_files), "filed_as": eml_filename,
        })

    except Exception as e:
        logger.error("Email send failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Email failed: {e}")


# ══════════════════════════════════════════════════════════════════════
# User email signature endpoints
# ══════════════════════════════════════════════════════════════════════

class SignatureUpdate(BaseModel):
    signature: str

@router.get("/disk/user-signature")
async def get_user_signature(request: Request):
    """Get the current user's email signature."""
    user = getattr(request.state, "current_user", None)
    if not user:
        raise HTTPException(status_code=403, detail="Not authenticated")
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text(
            "SELECT user_preferences FROM users WHERE id = :uid AND TRIM(tenant_id) = :tid"
        ), {"uid": user.id, "tid": tid})
        row = r.fetchone()
    prefs = (row[0] if row and row[0] else {}) or {}
    return JSONResponse({"signature": prefs.get("email_signature", "")})

@router.put("/disk/user-signature")
async def set_user_signature(request: Request, body: SignatureUpdate):
    """Save the current user's email signature."""
    user = getattr(request.state, "current_user", None)
    if not user:
        raise HTTPException(status_code=403, detail="Not authenticated")
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        await session.execute(sa_text("""
            UPDATE users SET user_preferences = COALESCE(user_preferences, '{}'::jsonb) || jsonb_build_object('email_signature', :sig),
                updated_at = NOW()
            WHERE id = :uid AND TRIM(tenant_id) = :tid
        """), {"sig": body.signature, "uid": user.id, "tid": tid})
        await session.commit()
    return JSONResponse({"status": "ok"})


# ══════════════════════════════════════════════════════════════════════
# Version link endpoint
# ══════════════════════════════════════════════════════════════════════

class VersionLinkRequest(BaseModel):
    new_doc_id: str
    parent_doc_id: str


@router.post("/disk/version-link")
async def version_link(request: Request, body: VersionLinkRequest):
    """Link a newly uploaded document as a new version of an existing doc."""
    tid = _tid(request)
    if not tid:
        raise HTTPException(status_code=403, detail="No tenant resolved")

    try:
        from modules.dms.services.dms_version_service import create_version_link

        result = await create_version_link(
            tenant_id=tid,
            new_doc_id=body.new_doc_id,
            parent_doc_id=body.parent_doc_id,
        )
        await _stamp_provenance(tid, body.new_doc_id, {
            "origin": "upload_linked",
            "created_via": "version_link",
            "linked_to_doc_id": body.parent_doc_id,
            "root_doc_id": result.get("chain_root_id"),
            "version_number": result.get("version_number"),
            "actor": str(_user_id(request)) if _user_id(request) is not None else None,
            "at": datetime.now(timezone.utc).isoformat(),
        })
        await _write_audit(tid, "document.version_created", result.get("chain_root_id"),
            user_uuid=_uid_uuid(request),
            new={"document_id": body.new_doc_id,
                 "version_number": result.get("version_number"),
                 "origin": "upload_linked"},
            details={"linked_to_doc_id": body.parent_doc_id})
        return JSONResponse({
            "status": "ok",
            "version_number": result["version_number"],
            "chain_root_id": result["chain_root_id"],
            "message": f"Linked as version {result['version_number']}",
        })
    except Exception as e:
        logger.error("Version link failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Version link failed: {e}")


# ══════════════════════════════════════════════════════════════════════
# Version chain endpoint
# ══════════════════════════════════════════════════════════════════════

@router.get("/disk/version-chain/{doc_id}")
async def version_chain(request: Request, doc_id: str):
    """Get all versions in a document's version chain."""
    tid = _tid(request)
    if not tid:
        raise HTTPException(status_code=403, detail="No tenant resolved")

    async with AsyncSessionLocal() as session:
        # Walk up to find root
        r = await session.execute(
            sa_text("""
                WITH RECURSIVE ancestors AS (
                    SELECT id, parent_doc_id, version_number, filename,
                           storage_path, file_size, checksum,
                           created_at, 0 AS depth
                    FROM documents
                    WHERE id = CAST(:did AS uuid)
                      AND TRIM(tenant_id) = :tid
                    UNION ALL
                    SELECT d.id, d.parent_doc_id, d.version_number, d.filename,
                           d.storage_path, d.file_size, d.checksum,
                           d.created_at, a.depth + 1
                    FROM documents d
                    JOIN ancestors a ON a.parent_doc_id = d.id
                    WHERE TRIM(d.tenant_id) = :tid
                      AND a.depth < 50
                )
                SELECT id::text FROM ancestors
                WHERE parent_doc_id IS NULL
                LIMIT 1
            """),
            {"did": doc_id, "tid": tid},
        )
        root_id = r.scalar()
        if not root_id:
            root_id = doc_id

        # Walk down from root to get full chain
        r2 = await session.execute(
            sa_text("""
                WITH RECURSIVE chain AS (
                    SELECT id, parent_doc_id, version_number, filename,
                           storage_path, file_size, checksum,
                           created_at::text AS created_at
                    FROM documents
                    WHERE id = CAST(:rid AS uuid)
                      AND TRIM(tenant_id) = :tid
                    UNION ALL
                    SELECT d.id, d.parent_doc_id, d.version_number, d.filename,
                           d.storage_path, d.file_size, d.checksum,
                           d.created_at::text
                    FROM documents d
                    JOIN chain c ON d.parent_doc_id = c.id
                    WHERE TRIM(d.tenant_id) = :tid
                )
                SELECT id::text AS document_id, parent_doc_id::text,
                       version_number, filename, storage_path,
                       file_size, checksum, created_at
                FROM chain
                ORDER BY version_number ASC
            """),
            {"rid": root_id, "tid": tid},
        )
        versions = [dict(row) for row in r2.mappings().fetchall()]

    return JSONResponse({
        "chain_root_id": root_id,
        "current_doc_id": doc_id,
        "versions": versions,
        "total_versions": len(versions),
    })


# ══════════════════════════════════════════════════════════════════════
# Compose / Reply / Forward — CONNECTOR PATH (EWS for internal users)
# ══════════════════════════════════════════════════════════════════════
# This endpoint routes through email_send_connector which detects the
# tenant's email provider (Exchange EWS, Office 365, Google Workspace)
# and sends via that provider.  For Exchange tenants, this means:
#   - Email appears in the user's Sent Items in Outlook
#   - Proper threading via In-Reply-To / References headers
#   - Attachments via EWS FileAttachment (not MIME relay)
#   - No Postfix hop — direct EWS send_and_save()
#
# The send is SYNCHRONOUS (no RQ queue) because EWS send_and_save()
# takes ~1-2 seconds and the user is waiting at the compose modal.
# Filing to the matter's Email folder is done via background RQ job
# so the response returns immediately after send.
# ══════════════════════════════════════════════════════════════════════

class SendEmailRequest(BaseModel):
    """Generalized email send: compose, reply, reply-all, forward."""
    to: str
    cc: Optional[str] = None
    bcc: Optional[str] = None
    subject: str = ""
    body_text: str = ""
    body_html: Optional[str] = None
    matter_id: Optional[str] = None
    # Attachments: list of relative paths within matter root
    attachment_paths: Optional[list] = None   # ["15-Email/doc.pdf", "02-Pleadings/motion.docx"]
    # Reply threading
    in_reply_to: Optional[str] = None         # Message-ID of email being replied to
    references: Optional[str] = None          # References header chain
    # Mode hint (informational — behavior is the same)
    mode: Optional[str] = "compose"           # compose | reply | reply_all | forward


@router.post("/disk/send-email")
async def send_email_endpoint(request: Request, body: SendEmailRequest):
    """Send an email via the tenant's email provider (EWS/SMTP).
    Sends synchronously for responsiveness. Files to matter via background job."""
    tid = _tid(request)
    if not tid:
        raise HTTPException(status_code=403, detail="No tenant resolved")

    root = None
    if body.matter_id:
        root = await _resolve_matter_root(tid, body.matter_id)
        if not root:
            raise HTTPException(status_code=404, detail="Matter disk root not found")

    # Validate and resolve attachment paths to absolute disk paths
    absolute_attachment_paths = []
    for rel_path in (body.attachment_paths or []):
        if not root:
            raise HTTPException(status_code=400, detail="matter_id required for attachments")
        fp = os.path.join(root, rel_path)
        rp = os.path.realpath(fp)
        if not rp.startswith(os.path.realpath(root)):
            raise HTTPException(status_code=403, detail=f"Path traversal: {rel_path}")
        if not os.path.isfile(rp):
            raise HTTPException(status_code=404, detail=f"Attachment not found: {rel_path}")
        absolute_attachment_paths.append(rp)

    # Get logged-in user
    user = getattr(request.state, "current_user", None)
    user_email = getattr(user, "email", None) if user else None
    user_name = getattr(user, "full_name", None) if user else None

    from_addr = user_email or "praesidium@hjmmlegal.com"
    mode_label = body.mode or "compose"

    try:
        # ── Send synchronously via connector (EWS or SMTP) ──────────
        from core.services.email_send_connector import send_email

        result = await send_email(
            tenant_id=tid,
            from_email=from_addr,
            to=body.to,
            subject=body.subject,
            body_html=body.body_html or "",
            body_text=body.body_text or "",
            cc=body.cc or "",
            bcc=body.bcc or "",
            in_reply_to=body.in_reply_to,
            references=body.references,
            attachment_paths=absolute_attachment_paths if absolute_attachment_paths else None,
        )

        send_method = result.get("method", "unknown")
        logger.info("Email [%s] sent via %s from %s to %s (%d attachments)",
                     mode_label, send_method, from_addr, body.to,
                     len(absolute_attachment_paths))

        # ── Enqueue filing to background (don't block response) ─────
        filed_as = None
        if body.matter_id:
            try:
                from redis import Redis
                from rq import Queue
                redis_url = os.environ.get("REDIS_URL", "redis://praesidium-redis:6379/0")
                _rparts = redis_url.replace("redis://", "").split("/")
                _rhp = _rparts[0].split(":")
                _rconn = Redis(host=_rhp[0], port=int(_rhp[1]) if len(_rhp) > 1 else 6379,
                               db=int(_rparts[1]) if len(_rparts) > 1 and _rparts[1] else 0)
                _q = Queue("default", connection=_rconn)

                file_payload = {
                    "tenant_id": tid,
                    "from_email": from_addr,
                    "to": body.to,
                    "cc": body.cc or "",
                    "subject": body.subject,
                    "body_html": body.body_html or "",
                    "body_text": body.body_text or "",
                    "in_reply_to": body.in_reply_to,
                    "references": body.references,
                    "matter_id": body.matter_id,
                    "attachment_paths": absolute_attachment_paths,
                    "mode": mode_label,
                }
                _q.enqueue("jobs.send_email_job.file_only", file_payload,
                           job_timeout=60, result_ttl=3600)
            except Exception as fe:
                logger.warning("Filing enqueue failed (email already sent): %s", fe)

        return JSONResponse({
            "status": "ok",
            "message": f"Email sent to {body.to}",
            "from": from_addr,
            "message_id": result.get("message_id", ""),
            "method": send_method,
            "attachments": len(absolute_attachment_paths),
            "mode": mode_label,
        })

    except HTTPException:
        raise
    except Exception as e:
        logger.error("send-email failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Email failed: {e}")


# ══════════════════════════════════════════════════════════════════════
# Stage attachment for email compose (drag-and-drop from desktop)
# ══════════════════════════════════════════════════════════════════════

@router.post("/disk/stage-attachment")
async def stage_attachment(
    request: Request,
    file: UploadFile = File(...),
    matter_id: str = Form(...),
):
    """Upload a file to a temp staging area within the matter.
    Returns the relative path that send-email can reference as an attachment."""
    tid = _tid(request)
    if not tid:
        raise HTTPException(status_code=403, detail="No tenant resolved")

    root = await _resolve_matter_root(tid, matter_id)
    if not root:
        raise HTTPException(status_code=404, detail="Matter disk root not found")

    # Stage to .email-staging/ inside matter root (hidden dir, cleaned periodically)
    stage_dir = os.path.join(root, ".email-staging")
    os.makedirs(stage_dir, exist_ok=True)

    filename = os.path.basename(file.filename or "attachment")
    content = await file.read()
    file_size = len(content)

    # Unique name to avoid collisions
    unique = str(_uuid.uuid4())[:8]
    staged_name = f"{unique}_{filename}"
    dest = os.path.join(stage_dir, staged_name)

    with open(dest, "wb") as f:
        f.write(content)

    rel_path = os.path.relpath(dest, root)  # e.g. ".email-staging/abc12345_report.pdf"
    logger.info("Staged attachment %s (%d bytes) for matter %s", staged_name, file_size, matter_id)

    return JSONResponse({
        "status": "ok",
        "filename": filename,
        "staged_name": staged_name,
        "path": rel_path,
        "size": file_size,
        "size_fmt": f"{file_size/1024:.1f} KB" if file_size < 1048576 else f"{file_size/1048576:.1f} MB",
    })


# ══════════════════════════════════════════════════════════════════════
# Save as New Version — called from OnlyOffice editor toolbar
# ══════════════════════════════════════════════════════════════════════
# Flow: OO forceSave() → oo-callback overwrites file → JS calls this
# endpoint → we snapshot the current file as a new versioned copy.
# ══════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════
# Register document — creates a documents row for an existing disk file
# ══════════════════════════════════════════════════════════════════════

class RegisterDocRequest(BaseModel):
    matter_id: str
    path: str          # matter-relative path
    filename: str


@router.post("/disk/register-doc")
async def register_doc(request: Request, body: RegisterDocRequest):
    """Register an existing file on disk in the documents table.
    Returns the document_id (existing or newly created)."""
    import urllib.parse as _up

    tid = _tid(request)
    if not tid:
        raise HTTPException(status_code=403, detail="No tenant resolved")

    root = await _resolve_matter_root(tid, body.matter_id)
    if not root:
        raise HTTPException(status_code=404, detail="Matter disk root not found")

    rel_path = _up.unquote(body.path)
    abs_path = os.path.realpath(os.path.join(root, rel_path))
    if not abs_path.startswith(os.path.realpath(root)):
        raise HTTPException(status_code=403, detail="Path traversal denied")
    if not os.path.isfile(abs_path):
        raise HTTPException(status_code=404, detail=f"File not found: {rel_path}")

    # Check if already registered
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT id::text AS document_id FROM documents
            WHERE storage_path = :sp AND TRIM(tenant_id) = :tid
            LIMIT 1
        """), {"sp": abs_path, "tid": tid})
        existing = r.scalar()

    if existing:
        return JSONResponse({"status": "ok", "document_id": existing, "created": False})

    # Create new row
    with open(abs_path, "rb") as f:
        content = f.read()
    file_size = len(content)
    checksum = hashlib.sha256(content).hexdigest()

    doc_id = str(_uuid.uuid4())
    mime_type = mimetypes.guess_type(body.filename)[0] or "application/octet-stream"
    user_id = _user_id(request)

    async with AsyncSessionLocal() as session:
        await session.execute(sa_text("""
            INSERT INTO documents (id, tenant_id, matter_id, filename, original_filename,
                mime_type, file_size, storage_path, checksum, version_number, status,
                created_by, created_at, updated_at)
            VALUES (CAST(:id AS uuid), :tid, CAST(:mid AS uuid), :fname, :fname,
                :mime, :fsize, :spath, :cs, 1, 'active', :uid, NOW(), NOW())
            ON CONFLICT DO NOTHING
        """), {"id": doc_id, "tid": tid, "mid": body.matter_id,
               "fname": body.filename, "mime": mime_type, "fsize": file_size,
               "spath": abs_path, "cs": checksum, "uid": user_id})
        await session.commit()

    await _stamp_provenance(tid, doc_id, {
        "origin": "registered",
        "created_via": "register_doc",
        "actor": str(user_id) if user_id is not None else None,
        "at": datetime.now(timezone.utc).isoformat(),
    })
    await _write_audit(tid, "document.registered", doc_id,
        user_uuid=_uid_uuid(request),
        new={"filename": body.filename, "file_size": file_size, "checksum": checksum},
        details=_actor(request))
    logger.info("register-doc: created %s for %s", doc_id, abs_path)
    return JSONResponse({"status": "ok", "document_id": doc_id, "created": True})


class SaveVersionRequest(BaseModel):
    matter_id: str
    path: str          # matter-relative path of the doc being edited
    filename: str      # display filename


def _working_copy_path(tid: str, abs_path: str) -> str:
    """Recovery/working-copy temp path written by the OO callback.
    MUST match onlyoffice_route._recovery_paths (md5 of the resolved path)."""
    import hashlib as _hl
    ext = abs_path.rsplit(".", 1)[-1].lower() if "." in abs_path else ""
    suffix = ("." + ext) if ext else ""
    base = os.environ.get("OO_RECOVERY_ROOT", "/tmp/praesidium-oo-recovery")
    return os.path.join(base, tid, _hl.md5(abs_path.encode()).hexdigest() + suffix)


@router.post("/disk/save-version")
async def save_version(request: Request, body: SaveVersionRequest):
    """Snapshot the current working copy as a new version (Model A / iManage).

    Working-copy model: the base file is untouched during editing — live edits
    live in the OO recovery temp. This reads that working copy and writes it as
    a new versioned file co-located with the chain ROOT, attached directly to
    the root (flat star). The opened/base file is not modified here; it commits
    on editor exit via the oo-callback.
    """
    import urllib.parse as _up

    tid = _tid(request)
    if not tid:
        raise HTTPException(status_code=403, detail="No tenant resolved")

    root = await _resolve_matter_root(tid, body.matter_id)
    if not root:
        raise HTTPException(status_code=404, detail="Matter disk root not found")

    rel_path = _up.unquote(body.path)
    abs_path = os.path.realpath(os.path.join(root, rel_path))
    if not abs_path.startswith(os.path.realpath(root)):
        raise HTTPException(status_code=403, detail="Path traversal denied")
    if not os.path.isfile(abs_path):
        raise HTTPException(status_code=404, detail=f"File not found: {rel_path}")

    # Current working content: the recovery temp if present & at least as new as
    # the base, otherwise the base file itself.
    working = _working_copy_path(tid, abs_path)
    src_for_version = abs_path
    try:
        if os.path.isfile(working) and os.path.getmtime(working) >= os.path.getmtime(abs_path):
            src_for_version = working
    except OSError:
        pass
    with open(src_for_version, "rb") as f:
        new_content = f.read()
    new_size = len(new_content)
    new_checksum = hashlib.sha256(new_content).hexdigest()

    # Resolve the original doc row (by storage_path, else matter+filename root).
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT id::text AS document_id, version_number
            FROM documents
            WHERE storage_path = :sp AND TRIM(tenant_id) = :tid
            ORDER BY created_at ASC LIMIT 1
        """), {"sp": abs_path, "tid": tid})
        orig = r.mappings().fetchone()
        if not orig:
            r2 = await session.execute(sa_text("""
                SELECT id::text AS document_id, version_number
                FROM documents
                WHERE matter_id = CAST(:mid AS uuid) AND TRIM(tenant_id) = :tid
                  AND filename = :fname AND parent_doc_id IS NULL
                ORDER BY created_at ASC LIMIT 1
            """), {"mid": body.matter_id, "tid": tid, "fname": body.filename})
            orig = r2.mappings().fetchone()

    original_doc_id = orig["document_id"] if orig else None
    if not original_doc_id:
        # Register the opened file as the chain root (v1) first.
        original_doc_id = str(_uuid.uuid4())
        base_cs = hashlib.sha256(open(abs_path, "rb").read()).hexdigest()
        base_sz = os.path.getsize(abs_path)
        mime0 = mimetypes.guess_type(body.filename)[0] or "application/octet-stream"
        async with AsyncSessionLocal() as session:
            await session.execute(sa_text("""
                INSERT INTO documents (id, tenant_id, matter_id, filename, original_filename,
                    mime_type, file_size, storage_path, checksum, version_number, status,
                    created_at, updated_at)
                VALUES (CAST(:id AS uuid), :tid, CAST(:mid AS uuid), :fname, :fname,
                    :mime, :fsize, :spath, :cs, 1, 'active', NOW(), NOW())
                ON CONFLICT DO NOTHING
            """), {"id": original_doc_id, "tid": tid, "mid": body.matter_id,
                   "fname": body.filename, "mime": mime0, "fsize": base_sz,
                   "spath": abs_path, "cs": base_cs})
            await session.commit()

    # Resolve chain ROOT + next version across the whole chain.
    async with AsyncSessionLocal() as session:
        r_root = await session.execute(sa_text("""
            WITH RECURSIVE ancestors AS (
                SELECT id, parent_doc_id, storage_path FROM documents
                WHERE id = CAST(:oid AS uuid) AND TRIM(tenant_id) = :tid
                UNION ALL
                SELECT d.id, d.parent_doc_id, d.storage_path FROM documents d
                JOIN ancestors a ON a.parent_doc_id = d.id WHERE TRIM(d.tenant_id) = :tid
            )
            SELECT id::text AS rid, storage_path FROM ancestors WHERE parent_doc_id IS NULL LIMIT 1
        """), {"oid": original_doc_id, "tid": tid})
        root_row = r_root.mappings().fetchone()
        root_doc_id = root_row["rid"] if root_row else original_doc_id
        root_sp = (root_row["storage_path"] if root_row else None) or abs_path

        r_max = await session.execute(sa_text("""
            WITH RECURSIVE chain AS (
                SELECT id, version_number FROM documents
                WHERE id = CAST(:rid AS uuid) AND TRIM(tenant_id) = :tid
                UNION ALL
                SELECT d.id, d.version_number FROM documents d
                JOIN chain c ON d.parent_doc_id = c.id WHERE TRIM(d.tenant_id) = :tid
            )
            SELECT COALESCE(MAX(version_number), 1) AS max_ver FROM chain
        """), {"rid": root_doc_id, "tid": tid})
        next_ver = (r_max.scalar() or 1) + 1

    # Write the new version, co-located with the ROOT.
    root_folder = os.path.dirname(root_sp)
    os.makedirs(root_folder, exist_ok=True)
    stem, ext = os.path.splitext(body.filename)
    versioned_name = f"{stem}.Ver.{next_ver}{ext}"
    versioned_path = os.path.join(root_folder, versioned_name)
    if os.path.exists(versioned_path):
        versioned_name = f"{stem}.Ver.{next_ver}.{str(_uuid.uuid4())[:6]}{ext}"
        versioned_path = os.path.join(root_folder, versioned_name)
    with open(versioned_path, "wb") as f:
        f.write(new_content)
    logger.info("save-version: wrote %s (v%d, parent=%s)", versioned_path, next_ver, root_doc_id)

    # Insert version row — flat star, parent = ROOT.
    new_doc_id = str(_uuid.uuid4())
    mime_type = mimetypes.guess_type(versioned_name)[0] or "application/octet-stream"
    user_id = _user_id(request)
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(sa_text("""
                INSERT INTO documents (id, tenant_id, matter_id, filename, original_filename,
                    mime_type, file_size, storage_path, checksum, version_number,
                    parent_doc_id, status, created_by, created_at, updated_at)
                VALUES (CAST(:id AS uuid), :tid, CAST(:mid AS uuid), :fname, :orig_fname,
                    :mime, :fsize, :spath, :cs, :ver,
                    CAST(:pid AS uuid), 'active', :uid, NOW(), NOW())
                ON CONFLICT DO NOTHING
            """), {"id": new_doc_id, "tid": tid, "mid": body.matter_id,
                   "fname": versioned_name, "orig_fname": body.filename,
                   "mime": mime_type, "fsize": new_size, "spath": versioned_path,
                   "cs": new_checksum, "ver": next_ver, "pid": root_doc_id, "uid": user_id})
            await session.commit()
    except Exception as e:
        logger.error("save-version doc insert failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Version save failed: {e}")

    # Register in dms_documents (indexing pipeline).
    try:
        dms_doc_id = str(_uuid.uuid4())
        async with AsyncSessionLocal() as session:
            await session.execute(sa_text("""
                INSERT INTO dms_documents (id, tenant_id, file_path, folder_root, file_hash,
                    file_size_bytes, extraction_status, source, updated_at)
                VALUES (CAST(:did AS uuid), :tid, :fpath, :froot, :fhash,
                    :fsize, 'pending', 'version', NOW())
                ON CONFLICT DO NOTHING
            """), {"did": dms_doc_id, "tid": tid, "fpath": versioned_path,
                   "froot": root, "fhash": new_checksum, "fsize": new_size})
            await session.commit()
    except Exception as e:
        logger.warning("save-version dms_documents insert (non-fatal): %s", e)

    # ── Provenance + audit (non-fatal) ───────────────────────────────
    _origin = "editor_save" if src_for_version == working else "copy"
    await _stamp_provenance(tid, new_doc_id, {
        "origin": _origin,
        "created_via": "save_version",
        "source_doc_id": original_doc_id,
        "root_doc_id": root_doc_id,
        "version_number": next_ver,
        "actor": str(user_id) if user_id is not None else None,
        "at": datetime.now(timezone.utc).isoformat(),
    })
    await _write_audit(tid, "document.version_created", root_doc_id,
        user_uuid=_uid_uuid(request),
        new={"document_id": new_doc_id, "version_number": next_ver,
             "filename": versioned_name, "origin": _origin},
        details={"source_doc_id": original_doc_id, "checksum": new_checksum})

    return JSONResponse({
        "status": "ok",
        "document_id": new_doc_id,
        "parent_doc_id": root_doc_id,
        "version_number": next_ver,
        "filename": versioned_name,
        "path": os.path.relpath(versioned_path, root),
        "checksum": new_checksum,
    })


# ══════════════════════════════════════════════════════════════════════
# Version reorder — renumber the chain to a user-supplied order
# ══════════════════════════════════════════════════════════════════════
# The version LINK (parent_doc_id) is the chain's source of truth; this only
# reassigns version_number to match the new order. Topology (the parent_doc_id
# IS NULL root) and filenames are left untouched -- chain members may carry any
# name. Logged to audit_log.
# ══════════════════════════════════════════════════════════════════════

class ReorderVersionsRequest(BaseModel):
    matter_id: Optional[str] = None
    ordered_doc_ids: list


async def _chain_root_and_members(session, tid, any_doc_id):
    """Return (root_id, {doc_id: version_number}) for the chain of any_doc_id."""
    r = await session.execute(sa_text("""
        WITH RECURSIVE ancestors AS (
            SELECT id, parent_doc_id FROM documents
            WHERE id = CAST(:did AS uuid) AND TRIM(tenant_id) = :tid
            UNION ALL
            SELECT d.id, d.parent_doc_id FROM documents d
            JOIN ancestors a ON a.parent_doc_id = d.id WHERE TRIM(d.tenant_id) = :tid
        )
        SELECT id::text FROM ancestors WHERE parent_doc_id IS NULL LIMIT 1
    """), {"did": any_doc_id, "tid": tid})
    root_id = r.scalar() or any_doc_id
    r2 = await session.execute(sa_text("""
        WITH RECURSIVE chain AS (
            SELECT id, version_number FROM documents
            WHERE id = CAST(:rid AS uuid) AND TRIM(tenant_id) = :tid
            UNION ALL
            SELECT d.id, d.version_number FROM documents d
            JOIN chain c ON d.parent_doc_id = c.id WHERE TRIM(d.tenant_id) = :tid
        )
        SELECT id::text AS document_id, version_number FROM chain
    """), {"rid": root_id, "tid": tid})
    members = {row["document_id"]: row["version_number"] for row in r2.mappings().fetchall()}
    return root_id, members


@router.post("/disk/reorder-versions")
async def reorder_versions(request: Request, body: ReorderVersionsRequest):
    tid = _tid(request)
    if not tid:
        raise HTTPException(status_code=403, detail="No tenant resolved")
    ordered = [str(x) for x in (body.ordered_doc_ids or [])]
    if len(ordered) < 2:
        raise HTTPException(status_code=400, detail="Need at least two versions to reorder")

    async with AsyncSessionLocal() as session:
        root_id, members = await _chain_root_and_members(session, tid, ordered[0])
        if set(ordered) != set(members.keys()):
            raise HTTPException(status_code=400,
                detail="ordered_doc_ids must be exactly the chain's members")
        old_map = {k: v for k, v in members.items()}
        for idx, did in enumerate(ordered, start=1):
            await session.execute(sa_text("""
                UPDATE documents SET version_number = :vn, updated_at = NOW()
                WHERE id = CAST(:did AS uuid) AND TRIM(tenant_id) = :tid
            """), {"vn": idx, "did": did, "tid": tid})
        await session.commit()

    new_map = {did: idx for idx, did in enumerate(ordered, start=1)}
    await _write_audit(tid, "document.versions_reordered", root_id,
        user_uuid=_uid_uuid(request),
        old={"version_numbers": old_map},
        new={"version_numbers": new_map},
        details={"order": ordered})

    return JSONResponse({"status": "ok", "chain_root_id": root_id,
                         "order": ordered, "count": len(ordered)})


# ══════════════════════════════════════════════════════════════════════
# Per-version comment — stored in documents.metadata.comment
# ══════════════════════════════════════════════════════════════════════

class VersionCommentRequest(BaseModel):
    document_id: str
    comment: str = ""


@router.post("/disk/set-version-comment")
async def set_version_comment(request: Request, body: VersionCommentRequest):
    tid = _tid(request)
    if not tid:
        raise HTTPException(status_code=403, detail="No tenant resolved")
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT (metadata->>'comment') AS old_comment FROM documents
            WHERE id = CAST(:did AS uuid) AND TRIM(tenant_id) = :tid
        """), {"did": body.document_id, "tid": tid})
        row = r.mappings().fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Document not found")
        old_comment = row["old_comment"]
        await session.execute(sa_text("""
            UPDATE documents
            SET metadata = COALESCE(metadata, '{}'::jsonb)
                          || jsonb_build_object('comment', CAST(:c AS text)),
                updated_at = NOW()
            WHERE id = CAST(:did AS uuid) AND TRIM(tenant_id) = :tid
        """), {"c": body.comment, "did": body.document_id, "tid": tid})
        await session.commit()

    await _write_audit(tid, "document.comment_updated", body.document_id,
        user_uuid=_uid_uuid(request),
        old={"comment": old_comment}, new={"comment": body.comment})
    return JSONResponse({"status": "ok", "document_id": body.document_id,
                         "comment": body.comment})


# ══════════════════════════════════════════════════════════════════════
# Document history — provenance + audit trail (feeds the Details tab)
# ══════════════════════════════════════════════════════════════════════

@router.get("/disk/doc-history/{doc_id}")
async def doc_history(request: Request, doc_id: str):
    tid = _tid(request)
    if not tid:
        raise HTTPException(status_code=403, detail="No tenant resolved")
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            WITH RECURSIVE ancestors AS (
                SELECT id, parent_doc_id FROM documents
                WHERE id = CAST(:did AS uuid) AND TRIM(tenant_id) = :tid
                UNION ALL
                SELECT d.id, d.parent_doc_id FROM documents d
                JOIN ancestors a ON a.parent_doc_id = d.id WHERE TRIM(d.tenant_id) = :tid
            )
            SELECT id::text FROM ancestors WHERE parent_doc_id IS NULL LIMIT 1
        """), {"did": doc_id, "tid": tid})
        root_id = r.scalar() or doc_id

        rv = await session.execute(sa_text("""
            WITH RECURSIVE chain AS (
                SELECT id, parent_doc_id, version_number, filename, storage_path,
                       metadata, created_at, updated_at, created_by FROM documents
                WHERE id = CAST(:rid AS uuid) AND TRIM(tenant_id) = :tid
                UNION ALL
                SELECT d.id, d.parent_doc_id, d.version_number, d.filename, d.storage_path,
                       d.metadata, d.created_at, d.updated_at, d.created_by FROM documents d
                JOIN chain c ON d.parent_doc_id = c.id WHERE TRIM(d.tenant_id) = :tid
            )
            SELECT c.id::text AS document_id, c.version_number, c.filename, c.storage_path,
                   (c.metadata->'provenance') AS provenance,
                   (c.metadata->>'comment') AS comment,
                   c.created_at::text AS created_at,
                   c.updated_at::text AS updated_at,
                   c.created_by,
                   u.full_name AS created_by_name
            FROM chain c
            LEFT JOIN users u ON u.id = c.created_by AND TRIM(u.tenant_id) = :tid
            ORDER BY c.version_number ASC
        """), {"rid": root_id, "tid": tid})
        versions = [dict(x) for x in rv.mappings().fetchall()]

        re_ = await session.execute(sa_text("""
            WITH RECURSIVE chain AS (
                SELECT id FROM documents
                WHERE id = CAST(:rid AS uuid) AND TRIM(tenant_id) = :tid
                UNION ALL
                SELECT d.id FROM documents d
                JOIN chain c ON d.parent_doc_id = c.id WHERE TRIM(d.tenant_id) = :tid
            )
            SELECT action, entity_id::text AS entity_id, user_id::text AS user_id,
                   old_values, new_values, details, created_at::text AS created_at
            FROM audit_log
            WHERE TRIM(tenant_id) = :tid AND entity_id IN (SELECT id FROM chain)
            ORDER BY created_at ASC
        """), {"rid": root_id, "tid": tid})
        events = [dict(x) for x in re_.mappings().fetchall()]

    return JSONResponse({"chain_root_id": root_id, "versions": versions, "events": events})


# ══════════════════════════════════════════════════════════════════════
# Version rename — DB-synced (disk + documents + dms_documents), link kept
# ══════════════════════════════════════════════════════════════════════

class VersionRenameRequest(BaseModel):
    document_id: str
    new_filename: str


@router.post("/disk/rename-version")
async def rename_version(request: Request, body: VersionRenameRequest):
    tid = _tid(request)
    if not tid:
        raise HTTPException(status_code=403, detail="No tenant resolved")
    new_name = os.path.basename((body.new_filename or "").strip())
    if not new_name or new_name in (".", ".."):
        raise HTTPException(status_code=400, detail="Invalid name")
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT storage_path, filename FROM documents
            WHERE id = CAST(:did AS uuid) AND TRIM(tenant_id) = :tid
        """), {"did": body.document_id, "tid": tid})
        row = r.mappings().fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Document not found")
        old_path = row["storage_path"]
        old_name = row["filename"]
        new_path = os.path.join(os.path.dirname(old_path), new_name)
        if not os.path.realpath(new_path).startswith(PRAESIDIUM_ROOT):
            raise HTTPException(status_code=403, detail="Path denied")
        if os.path.exists(new_path):
            raise HTTPException(status_code=409, detail="A file with that name already exists")
        if not os.path.isfile(old_path):
            raise HTTPException(status_code=404, detail="File not found on disk")
        try:
            os.rename(old_path, new_path)
        except Exception as e:
            raise HTTPException(status_code=500, detail="Disk rename failed: %s" % e)
        await session.execute(sa_text("""
            UPDATE documents SET filename = :fn, storage_path = :sp, updated_at = NOW()
            WHERE id = CAST(:did AS uuid) AND TRIM(tenant_id) = :tid
        """), {"fn": new_name, "sp": new_path, "did": body.document_id, "tid": tid})
        await session.execute(sa_text("""
            UPDATE dms_documents SET file_path = :sp
            WHERE file_path = :op AND TRIM(tenant_id) = :tid
        """), {"sp": new_path, "op": old_path, "tid": tid})
        await session.commit()
    await _write_audit(tid, "document.renamed", body.document_id,
        old={"filename": old_name}, new={"filename": new_name})
    return JSONResponse({"status": "ok", "document_id": body.document_id,
                         "filename": new_name, "path": new_path})


# ══════════════════════════════════════════════════════════════════════
# Version delete — removes file + rows; refuses to delete a root with kids
# ══════════════════════════════════════════════════════════════════════

class VersionDeleteRequest(BaseModel):
    document_id: str


@router.post("/disk/delete-version")
async def delete_version(request: Request, body: VersionDeleteRequest):
    tid = _tid(request)
    if not tid:
        raise HTTPException(status_code=403, detail="No tenant resolved")
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT storage_path, filename, parent_doc_id::text AS parent, version_number
            FROM documents WHERE id = CAST(:did AS uuid) AND TRIM(tenant_id) = :tid
        """), {"did": body.document_id, "tid": tid})
        row = r.mappings().fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Document not found")
        if row["parent"] is None:
            rc = await session.execute(sa_text("""
                SELECT count(*) AS n FROM documents
                WHERE parent_doc_id = CAST(:did AS uuid) AND TRIM(tenant_id) = :tid
            """), {"did": body.document_id, "tid": tid})
            if (rc.scalar() or 0) > 0:
                raise HTTPException(status_code=409,
                    detail="Cannot delete the base version while other versions exist. Delete the others first or re-root the chain.")
        path = row["storage_path"]
        fname = row["filename"]
        try:
            if path and os.path.isfile(path):
                os.remove(path)
        except Exception:
            pass
        await session.execute(sa_text("""
            DELETE FROM dms_documents WHERE file_path = :sp AND TRIM(tenant_id) = :tid
        """), {"sp": path, "tid": tid})
        await session.execute(sa_text("""
            DELETE FROM documents WHERE id = CAST(:did AS uuid) AND TRIM(tenant_id) = :tid
        """), {"did": body.document_id, "tid": tid})
        await session.commit()
    await _write_audit(tid, "document.version_deleted", row["parent"] or body.document_id,
        old={"filename": fname, "version_number": row["version_number"]})
    return JSONResponse({"status": "ok", "deleted": body.document_id})


# ======================================================================
# Link an existing document as a version of another existing document
# (retro / cleanup path -- distinct from the post-upload /disk/version-link)
# ======================================================================
# Use case: two files were uploaded separately as standalone docs; later
# during cleanup you fold one into the other's version chain. Attaches the
# source as a new version of the TARGET's chain ROOT (flat star, consistent
# with save-version). Files are NOT moved on disk -- only the logical link
# changes. Refuses self-links, cycles, and sources that already have their
# own child versions.
# ======================================================================

class LinkExistingVersionRequest(BaseModel):
    source_doc_id: str          # the standalone doc to fold in
    target_doc_id: str          # any member of the destination chain


@router.post("/disk/link-existing-version")
async def link_existing_version(request: Request, body: LinkExistingVersionRequest):
    tid = _tid(request)
    if not tid:
        raise HTTPException(status_code=403, detail="No tenant resolved")
    src = str(body.source_doc_id)
    tgt = str(body.target_doc_id)
    if src == tgt:
        raise HTTPException(status_code=400, detail="A document cannot be a version of itself")

    async with AsyncSessionLocal() as session:
        # Both docs must exist in this tenant; capture prior state of source.
        r = await session.execute(sa_text("""
            SELECT id::text AS id, parent_doc_id::text AS parent,
                   version_number, filename
            FROM documents
            WHERE id IN (CAST(:src AS uuid), CAST(:tgt AS uuid))
              AND TRIM(tenant_id) = :tid
        """), {"src": src, "tgt": tgt, "tid": tid})
        rows = {x["id"]: x for x in r.mappings().fetchall()}
        if src not in rows or tgt not in rows:
            raise HTTPException(status_code=404, detail="Source or target document not found")
        old_parent = rows[src]["parent"]
        old_ver = rows[src]["version_number"]

        # Refuse to fold a source that already has its own child versions.
        rc = await session.execute(sa_text("""
            SELECT count(*) AS n FROM documents
            WHERE parent_doc_id = CAST(:src AS uuid) AND TRIM(tenant_id) = :tid
        """), {"src": src, "tid": tid})
        if (rc.scalar() or 0) > 0:
            raise HTTPException(status_code=409,
                detail="Source has its own versions. Re-root or detach those first.")

        # Resolve the TARGET chain root.
        r2 = await session.execute(sa_text("""
            WITH RECURSIVE ancestors AS (
                SELECT id, parent_doc_id FROM documents
                WHERE id = CAST(:tgt AS uuid) AND TRIM(tenant_id) = :tid
                UNION ALL
                SELECT d.id, d.parent_doc_id FROM documents d
                JOIN ancestors a ON a.parent_doc_id = d.id WHERE TRIM(d.tenant_id) = :tid
            )
            SELECT id::text FROM ancestors WHERE parent_doc_id IS NULL LIMIT 1
        """), {"tgt": tgt, "tid": tid})
        root_id = r2.scalar() or tgt

        # Cycle guard: target's root must not be the source itself.
        if root_id == src:
            raise HTTPException(status_code=409,
                detail="Cannot link: target is already in the source's chain")

        # Next version number across the target chain.
        r3 = await session.execute(sa_text("""
            WITH RECURSIVE chain AS (
                SELECT id, version_number FROM documents
                WHERE id = CAST(:rid AS uuid) AND TRIM(tenant_id) = :tid
                UNION ALL
                SELECT d.id, d.version_number FROM documents d
                JOIN chain c ON d.parent_doc_id = c.id WHERE TRIM(d.tenant_id) = :tid
            )
            SELECT COALESCE(MAX(version_number), 1) AS mx FROM chain
        """), {"rid": root_id, "tid": tid})
        next_ver = (r3.scalar() or 1) + 1

        # Re-parent the source onto the root (flat star).
        await session.execute(sa_text("""
            UPDATE documents
            SET parent_doc_id = CAST(:rid AS uuid), version_number = :ver, updated_at = NOW()
            WHERE id = CAST(:src AS uuid) AND TRIM(tenant_id) = :tid
        """), {"rid": root_id, "ver": next_ver, "src": src, "tid": tid})
        await session.commit()

    await _stamp_provenance(tid, src, {
        "origin": "retro_version_link",
        "created_via": "link_existing_version",
        "linked_to_root_id": root_id,
        "target_doc_id": tgt,
        "version_number": next_ver,
        "actor": str(_user_id(request)) if _user_id(request) is not None else None,
        "at": datetime.now(timezone.utc).isoformat(),
    })
    await _write_audit(tid, "document.version_linked", root_id,
        user_uuid=_uid_uuid(request),
        old={"document_id": src, "parent_doc_id": old_parent, "version_number": old_ver},
        new={"document_id": src, "parent_doc_id": root_id, "version_number": next_ver},
        details={"source_doc_id": src, "target_doc_id": tgt, "mode": "retro_cleanup", **_actor(request)})

    return JSONResponse({"status": "ok", "chain_root_id": root_id,
                         "source_doc_id": src, "version_number": next_ver})


# ======================================================================
# Detach a version from its chain -- the document becomes standalone.
# Files are NOT moved on disk; only the logical link changes. The base
# version (chain root) cannot be unlinked, nor can a version that has
# its own children.
# ======================================================================

class VersionUnlinkRequest(BaseModel):
    document_id: str


@router.post("/disk/unlink-version")
async def unlink_version(request: Request, body: VersionUnlinkRequest):
    tid = _tid(request)
    if not tid:
        raise HTTPException(status_code=403, detail="No tenant resolved")
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT parent_doc_id::text AS parent, version_number, filename
            FROM documents WHERE id = CAST(:did AS uuid) AND TRIM(tenant_id) = :tid
        """), {"did": body.document_id, "tid": tid})
        row = r.mappings().fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Document not found")
        if row["parent"] is None:
            raise HTTPException(status_code=409,
                detail="This is the base version; it is not linked to anything")
        rc = await session.execute(sa_text("""
            SELECT count(*) AS n FROM documents
            WHERE parent_doc_id = CAST(:did AS uuid) AND TRIM(tenant_id) = :tid
        """), {"did": body.document_id, "tid": tid})
        if (rc.scalar() or 0) > 0:
            raise HTTPException(status_code=409,
                detail="This version has its own child versions; detach those first")
        await session.execute(sa_text("""
            UPDATE documents
            SET parent_doc_id = NULL, version_number = 1, updated_at = NOW()
            WHERE id = CAST(:did AS uuid) AND TRIM(tenant_id) = :tid
        """), {"did": body.document_id, "tid": tid})
        await session.commit()
    await _write_audit(tid, "document.version_unlinked", row["parent"],
        user_uuid=_uid_uuid(request),
        old={"document_id": body.document_id, "parent_doc_id": row["parent"],
             "version_number": row["version_number"]},
        new={"document_id": body.document_id, "parent_doc_id": None, "version_number": 1},
        details={"filename": row["filename"], **_actor(request)})
    return JSONResponse({"status": "ok", "document_id": body.document_id})
