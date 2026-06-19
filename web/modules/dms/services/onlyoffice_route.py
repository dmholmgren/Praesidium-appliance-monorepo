"""
OnlyOffice Editor API — v4
===========================
Adds support for staging paths (chats/) in addition to matter paths.

When path starts with 'chats/', resolves against /mnt/praesidium/{tid}/chats/
instead of the matter's disk root. This lets the drafting UI open staged
documents in OnlyOffice directly, with save-back to the staging file.
"""
from __future__ import annotations
import os, logging, hashlib, json, time, hmac, base64, shutil, urllib.parse
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, FileResponse
from sqlalchemy import text as sa_text
from core.db.base import AsyncSessionLocal
import httpx

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/dms/matter", tags=["onlyoffice-api"])

PRAESIDIUM_ROOT = "/mnt/praesidium"
OO_JWT_SECRET = os.environ.get("OO_JWT_SECRET", "praesidium-oo-jwt-2026")
OO_INTERNAL = os.environ.get("OO_INTERNAL_URL", "http://172.28.0.20")
WEB_INTERNAL = os.environ.get("OO_INTERNAL_WEB_URL", "http://172.28.0.5:8000")


def _tid(r):
    return (getattr(r.state, "tenant_id", "") or "").strip()

def _safe_path(root, rel):
    rel = urllib.parse.unquote(rel)
    resolved = os.path.realpath(os.path.join(root, rel))
    if not resolved.startswith(os.path.realpath(root)):
        raise HTTPException(status_code=403, detail="Path traversal denied")
    return resolved

def _matter_disk_root(tid, client, matter):
    if not client or not matter: return None
    p = os.path.join(PRAESIDIUM_ROOT, tid, "matters", client, matter)
    return p if os.path.isdir(p) else None

async def _resolve_root(tid, mid):
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT m.matter_name, c.client_name
            FROM matters m LEFT JOIN clients c ON m.client_id = c.id
                AND trim(m.tenant_id) = trim(c.tenant_id)
            WHERE m.id = CAST(:mid AS uuid) AND trim(m.tenant_id) = trim(:tid)
        """), {"mid": mid, "tid": tid})
        row = r.mappings().fetchone()
    if not row: raise HTTPException(status_code=404, detail="Matter not found")
    root = _matter_disk_root(tid, row["client_name"], row["matter_name"])
    return root, row

async def _resolve_root_by_matter(mid: str):
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT m.matter_name, c.client_name, TRIM(m.tenant_id) as tid
            FROM matters m LEFT JOIN clients c ON m.client_id = c.id
                AND trim(m.tenant_id) = trim(c.tenant_id)
            WHERE m.id = CAST(:mid AS uuid)
        """), {"mid": mid})
        row = r.mappings().fetchone()
    if not row: raise HTTPException(status_code=404, detail="Matter not found")
    root = _matter_disk_root(row["tid"], row["client_name"], row["matter_name"])
    return root, row


def _resolve_file_path(tid: str, path: str, matter_root: str | None) -> str:
    """Resolve a file path that could be either:
    - A matter-relative path: '02-Pleadings/file.docx' → {matter_root}/02-Pleadings/file.docx
    - A staging path: 'chats/session123/file.docx' → /mnt/praesidium/{tid}/chats/session123/file.docx
    - An absolute path: '/mnt/praesidium/{tid}/...' → normalized to relative form first
    Returns the absolute filesystem path."""
    path = urllib.parse.unquote(path)
    
    # Normalize absolute paths to relative form (safety net — frontend should send relative)
    tenant_prefix = f"/mnt/praesidium/{tid}/"
    if path.startswith(tenant_prefix):
        path = path[len(tenant_prefix):]
    elif path.startswith("/mnt/praesidium/"):
        parts = path.split("/", 4)  # ['', 'mnt', 'praesidium', '{tid}', 'rest...']
        if len(parts) >= 5:
            path = parts[4]
    
    if path.startswith("chats/"):
        # Staging path — resolve against tenant root
        tenant_root = os.path.join(PRAESIDIUM_ROOT, tid)
        fp = os.path.realpath(os.path.join(tenant_root, path))
        if not fp.startswith(os.path.realpath(tenant_root)):
            raise HTTPException(status_code=403, detail="Path traversal denied")
        return fp
    elif matter_root:
        # Matter-relative path
        return _safe_path(matter_root, path)
    else:
        raise HTTPException(status_code=404, detail="No disk root and not a staging path")


def _sign_jwt(payload: dict) -> str:
    header = base64.urlsafe_b64encode(json.dumps(
        {"alg": "HS256", "typ": "JWT"}
    ).encode()).rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(
        json.dumps(payload).encode()
    ).rstrip(b"=").decode()
    signing_input = f"{header}.{body}"
    sig = hmac.new(
        OO_JWT_SECRET.encode(), signing_input.encode(), hashlib.sha256
    ).digest()
    signature = base64.urlsafe_b64encode(sig).rstrip(b"=").decode()
    return f"{header}.{body}.{signature}"

def _file_ext(path):
    return path.rsplit(".", 1)[-1].lower() if "." in path else ""

def _doc_type(ext):
    if ext in ("doc", "docx", "odt", "rtf", "txt"): return "word"
    if ext in ("xls", "xlsx", "ods", "csv"): return "cell"
    if ext in ("ppt", "pptx", "odp"): return "slide"
    return "word"


def _oo_keyfile(fp):
    d = os.path.join(OO_RECOVERY_ROOT, "ookeys")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, hashlib.md5(fp.encode()).hexdigest())


def _oo_gen(fp, bump=False):
    """Stable co-editing generation token for a file. Persists in the recovery
    area (not the DMS folder). Bumped only on deliberate out-of-band replacement
    (restore / reattach) so co-editors share one key during a live session but
    get a fresh key after the base is swapped underneath them."""
    kf = _oo_keyfile(fp)
    if not bump and os.path.isfile(kf):
        try:
            return open(kf).read().strip()
        except Exception:
            pass
    import uuid as _uuid
    tok = _uuid.uuid4().hex
    try:
        with open(kf, "w") as f:
            f.write(tok)
    except Exception:
        pass
    return tok


def bump_oo_gen(fp):
    """Force a new co-editing key (call after restore/reattach overwrite)."""
    return _oo_gen(fp, bump=True)


def _doc_key(fp, coedit):
    """OnlyOffice document key. For co-editing we want a key that is STABLE during
    a session (so collaborators join the same doc) and only changes on deliberate
    replacement -- the mtime/size key would split late joiners after any save."""
    if coedit:
        return hashlib.md5(("coedit:" + fp + ":" + _oo_gen(fp)).encode()).hexdigest()
    return hashlib.md5(
        ("%s:%s:%s" % (fp, os.path.getmtime(fp), os.path.getsize(fp))).encode()
    ).hexdigest()


# ═══════════════════════════════════════════════════════════════
# EDITOR CONFIG (browser-facing, has auth)
# ═══════════════════════════════════════════════════════════════

@router.get("/{matter_id}/oo-config")
async def oo_editor_config(request: Request, matter_id: str, path: str = "",
                            mode: str = "edit", coedit: str = ""):
    tid = _tid(request)
    if not path:
        raise HTTPException(status_code=400, detail="path required")
    
    # Resolve matter root (may be None for staging-only access)
    try:
        root, _ = await _resolve_root(tid, matter_id)
    except HTTPException:
        root = None

    fp = _resolve_file_path(tid, path, root)
    if not os.path.isfile(fp):
        raise HTTPException(status_code=404, detail=f"File not found: {fp}")

    ext = _file_ext(fp)
    filename = os.path.basename(fp)
    file_key = _doc_key(fp, coedit)

    enc_path = urllib.parse.quote(path, safe="")
    download_url = f"{WEB_INTERNAL}/api/v1/dms/matter/{matter_id}/oo-download?path={enc_path}"
    callback_url = f"{WEB_INTERNAL}/api/v1/dms/matter/{matter_id}/oo-callback?path={enc_path}"

    user = getattr(request.state, "current_user", None)
    user_id = str(getattr(user, "id", "0") or "0")
    user_name = getattr(user, "full_name", None) or getattr(user, "username", "User")

    config = {
        "document": {
            "fileType": ext if ext else "docx",
            "key": file_key,
            "title": filename,
            "url": download_url,
            "permissions": {
                "chat": False,
                "comment": True,
                "download": True,
                "edit": mode == "edit",
                "print": True,
                "review": True,
            },
        },
        "documentType": _doc_type(ext),
        "editorConfig": {
            "callbackUrl": callback_url,
            "lang": "en",
            "mode": mode,
            "user": {"id": user_id, "name": user_name},
            "customization": {
                "autosave": True,
                "compactHeader": True,
                "compactToolbar": False,
                "feedback": False,
                "forcesave": True,
                "help": False,
                "hideRightMenu": False,
                "toolbarNoTabs": False,
            },
        },
        "type": "desktop",
        "height": "100%",
        "width": "100%",
    }

    if coedit:
        config["editorConfig"]["coediting"] = {"mode": "fast", "change": True}
    token = _sign_jwt(config)
    config["token"] = token

    return JSONResponse({
        "config": config,
        "api_url": "/oo/web-apps/apps/api/documents/api.js",
    })


# ═══════════════════════════════════════════════════════════════
# FILE DOWNLOAD (OO-facing, NO auth — internal Docker bridge)
# ═══════════════════════════════════════════════════════════════

@router.get("/{matter_id}/oo-download")
async def oo_download(request: Request, matter_id: str, path: str = ""):
    if not path:
        raise HTTPException(status_code=400, detail="path required")
    
    # Resolve tenant from matter (OO calls have no auth context)
    root, row = await _resolve_root_by_matter(matter_id)
    tid = row["tid"]
    
    fp = _resolve_file_path(tid, path, root)
    logger.info("OO download: path=%s resolved=%s exists=%s", path, fp, os.path.isfile(fp))
    if not os.path.isfile(fp):
        raise HTTPException(status_code=404, detail=f"File not found: {fp}")
    return FileResponse(fp, media_type="application/octet-stream",
                        filename=os.path.basename(fp))


# ═══════════════════════════════════════════════════════════════
# SAVE CALLBACK (OO-facing, NO auth — saves back to same file)
# ═══════════════════════════════════════════════════════════════


# ===============================================================
# SAVE CALLBACK (OO-facing, NO auth)
#   autosave/forcesave-timer -> recovery temp; base only on deliberate save
# ===============================================================

# Crash-recovery temp (Word-style): forcesave/autosave writes here; the base
# (version of record) only changes on a deliberate save.
OO_RECOVERY_ROOT = os.environ.get("OO_RECOVERY_ROOT", "/tmp/praesidium-oo-recovery")

# OnlyOffice forcesavetype enum (c_oAscForceSaveTypes):
#   0 = Command  (programmatic /command forcesave)
#   1 = Button   (user pressed Save / Ctrl+S)   -> commit to base
#   2 = Timeout  (autosave timer)               -> recovery temp only
#   3 = Form     (form submit)
OO_FORCESAVE_BUTTON = 1


def _recovery_paths(tid: str, fp: str):
    """Return (recovery_file, manifest_file) for a given resolved base path.
    Keyed by a hash of the resolved path so it is stable across editing
    sessions (the OO document key is not — it changes with mtime/size)."""
    ext = _file_ext(fp)
    suffix = ("." + ext) if ext else ""
    key = hashlib.md5(fp.encode()).hexdigest()
    d = os.path.join(OO_RECOVERY_ROOT, tid)
    os.makedirs(d, exist_ok=True)
    rec = os.path.join(d, key + suffix)
    return rec, rec + ".json"


def _clear_recovery(*paths):
    for p in paths:
        try:
            if p and os.path.isfile(p):
                os.remove(p)
        except Exception:
            pass


@router.post("/{matter_id}/oo-callback")
async def oo_callback(request: Request, matter_id: str, path: str = ""):
    if not path:
        return JSONResponse({"error": 1})
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": 1})

    status = body.get("status", 0)
    forcesavetype = body.get("forcesavetype")
    logger.info("OO callback: matter=%s path=%s status=%s forcesavetype=%s",
                matter_id, path, status, forcesavetype)

    # 1 = editing, 3 = save error, 7 = forcesave error -> nothing to write
    if status not in (2, 4, 6):
        return JSONResponse({"error": 0})

    try:
        root, row = await _resolve_root_by_matter(matter_id)
        tid = row["tid"]
        fp = _resolve_file_path(tid, path, root)
    except Exception as e:
        logger.exception("OO callback resolve error: %s", e)
        return JSONResponse({"error": 1})

    rec_path, rec_manifest = _recovery_paths(tid, fp)

    # status 4 = closed with no changes -> discard any stale recovery temp
    if status == 4:
        _clear_recovery(rec_path, rec_manifest)
        return JSONResponse({"error": 0})

    download_url = body.get("url")
    if not download_url:
        return JSONResponse({"error": 1})

    # Rewrite OO download URL from public (/oo/ proxy) to internal Docker address.
    import re as _re
    download_url = _re.sub(r'https?://[^/]+/oo/', OO_INTERNAL + '/', download_url, count=1)
    if download_url.startswith('https://') or download_url.startswith('http://login') or download_url.startswith('http://10.'):
        download_url = _re.sub(r'https?://[^/]+/', OO_INTERNAL + '/', download_url, count=1)

    # Working-copy model: the base (version of record) changes ONLY on exit —
    # status 2, i.e. the last editor has closed the document. Every in-edit
    # autosave / force-save (status 6, any forcesavetype) goes to the recovery
    # temp and never touches the base. Status 2 is also the OnlyOffice
    # co-editing save point (consolidated doc when the last collaborator exits).
    _is_brief = False
    _brief_cid = None
    _brief_doc_id = None
    try:
        async with AsyncSessionLocal() as _bs0:
            _br0 = await _bs0.execute(sa_text(
                "SELECT d.id::text AS doc_id, ac.id::text AS cid FROM documents d "
                "JOIN appellate_cases ac ON ac.matter_id=d.matter_id "
                "  AND TRIM(ac.tenant_id)=TRIM(d.tenant_id) "
                "WHERE d.storage_path=:sp AND TRIM(d.tenant_id)=:tid "
                "  AND d.document_type='brief' LIMIT 1"), {"sp": fp, "tid": tid})
            _brow0 = _br0.mappings().fetchone()
        if _brow0:
            _is_brief = True
            _brief_cid = _brow0["cid"]
            _brief_doc_id = _brow0["doc_id"]
    except Exception as _bde:
        logger.warning("OO: brief detect failed (non-fatal): %s", _bde)

    # Save-button base-commit is scoped to briefs; other DMS docs commit on exit only.
    commit_to_base = (status == 2) or (_is_brief and status == 6 and forcesavetype == OO_FORCESAVE_BUTTON)

    try:
        async with httpx.AsyncClient(timeout=60, verify=False) as client:
            resp = await client.get(download_url)
        if resp.status_code != 200:
            logger.error("OO: download failed HTTP %d", resp.status_code)
            return JSONResponse({"error": 1})
        content = resp.content
    except Exception as e:
        logger.exception("OO callback fetch error: %s", e)
        return JSONResponse({"error": 1})

    # Always refresh the recovery temp so it reflects the latest editor state.
    try:
        with open(rec_path, "wb") as f:
            f.write(content)
        with open(rec_manifest, "w") as f:
            json.dump({
                "original": fp, "matter_id": matter_id, "path": path,
                "status": status, "forcesavetype": forcesavetype,
                "bytes": len(content), "saved_at": int(time.time()),
            }, f)
    except Exception as e:
        logger.warning("OO: recovery temp write failed: %s", e)

    if not commit_to_base:
        logger.info("OO: autosave -> recovery temp (%d bytes) %s", len(content), rec_path)
        return JSONResponse({"error": 0})

    # Fingerprint the prior content before overwrite (for the edit log).
    old_checksum = None
    old_size = None
    try:
        if os.path.isfile(fp):
            with open(fp, "rb") as _old:
                _ob = _old.read()
            old_size = len(_ob)
            old_checksum = hashlib.sha256(_ob).hexdigest()
    except Exception:
        pass

    # Commit to base (version of record). Keep the existing .versions/*.bak net.
    try:
        parent = os.path.dirname(fp)
        backup_dir = os.path.join(parent, ".versions")
        os.makedirs(backup_dir, exist_ok=True)
        ts = int(time.time())
        if os.path.isfile(fp):
            shutil.copy2(fp, os.path.join(backup_dir, "%s.%d.bak" % (os.path.basename(fp), ts)))
        with open(fp, "wb") as f:
            f.write(content)
        logger.info("OO: committed %d bytes -> BASE %s (status=%s fst=%s)",
                    len(content), fp, status, forcesavetype)
    except Exception as e:
        logger.exception("OO callback base-commit error: %s", e)
        return JSONResponse({"error": 1})

    # Log the commit as a 'document.edited' event (only when content actually
    # changed) and keep the documents row in step with disk. Non-fatal.
    try:
        new_checksum = hashlib.sha256(content).hexdigest()
        if new_checksum != old_checksum:
            actor_id = None
            _users = body.get("users") or []
            if isinstance(_users, list) and _users:
                _u0 = _users[0]
                actor_id = _u0.get("userid") if isinstance(_u0, dict) else _u0

            # Resolve the doc + keep the row in step with disk (own session/try).
            doc_id = None
            try:
                async with AsyncSessionLocal() as session:
                    rr = await session.execute(sa_text("""
                        SELECT id::text AS id FROM documents
                        WHERE storage_path = :sp AND TRIM(tenant_id) = :tid
                        ORDER BY created_at ASC LIMIT 1
                    """), {"sp": fp, "tid": tid})
                    doc_id = rr.scalar()
                    if doc_id:
                        await session.execute(sa_text("""
                            UPDATE documents SET checksum = :cs, file_size = :sz, updated_at = NOW()
                            WHERE id = CAST(:id AS uuid) AND TRIM(tenant_id) = :tid
                        """), {"cs": new_checksum, "sz": len(content), "id": doc_id, "tid": tid})
                        await session.commit()
            except Exception as e:
                logger.warning("OO: edit row update failed (non-fatal): %s", e)

            # Best-effort actor name (int param, no cast) -- never blocks audit.
            actor_name = None
            try:
                if actor_id is not None and str(actor_id).isdigit():
                    async with AsyncSessionLocal() as session:
                        ru = await session.execute(sa_text("""
                            SELECT full_name FROM users
                            WHERE id = :uid AND TRIM(tenant_id) = :tid
                        """), {"uid": int(actor_id), "tid": tid})
                        actor_name = ru.scalar()
            except Exception:
                actor_name = None

            if doc_id:
                from modules.dms.services.dms_upload_route import _write_audit
                await _write_audit(tid, "document.edited", doc_id,
                    old={"size": old_size, "checksum": (old_checksum or "")[:16]},
                    new={"size": len(content), "checksum": new_checksum[:16]},
                    details={"bytes_delta": len(content) - (old_size or 0),
                             "actor_user_id": str(actor_id) if actor_id is not None else None,
                             "actor_name": actor_name})
    except Exception as e:
        logger.warning("OO: edit audit log failed (non-fatal): %s", e)

    # Brief workspace: record a version row when a brief DOCX is committed to base.
    if _is_brief:
        try:
            _actor = None
            _us = body.get("users") or []
            if isinstance(_us, list) and _us:
                _u0 = _us[0]
                _actor = _u0.get("userid") if isinstance(_u0, dict) else _u0
            # OnlyOffice changes zip (per-author colored changes) -> kept for diffing.
            _changes_path = None
            _cu = body.get("changesurl")
            if _cu:
                try:
                    _cu2 = _re.sub(r'https?://[^/]+/oo/', OO_INTERNAL + '/', _cu, count=1)
                    if _cu2.startswith('https://') or _cu2.startswith('http://login') or _cu2.startswith('http://10.'):
                        _cu2 = _re.sub(r'https?://[^/]+/', OO_INTERNAL + '/', _cu2, count=1)
                    async with httpx.AsyncClient(timeout=60, verify=False) as _ccl:
                        _ccr = await _ccl.get(_cu2)
                    if _ccr.status_code == 200:
                        _cdir = os.path.join(OO_RECOVERY_ROOT, tid, "changes")
                        os.makedirs(_cdir, exist_ok=True)
                        _changes_path = os.path.join(
                            _cdir, hashlib.md5((fp + ":" + str(time.time())).encode()).hexdigest() + ".zip")
                        with open(_changes_path, "wb") as _cf:
                            _cf.write(_ccr.content)
                except Exception as _cce:
                    logger.warning("OO: changes zip fetch failed (non-fatal): %s", _cce)
            _kind = "save" if (status == 6 and forcesavetype == OO_FORCESAVE_BUTTON) else "exit-save"
            from fastapi.concurrency import run_in_threadpool as _ritp
            from modules.depositions.routes.brief_workspace_api import _record_oo_version
            await _ritp(_record_oo_version, tid, _brief_cid, _brief_doc_id, fp,
                        _changes_path, _actor, _kind)
        except Exception as _be:
            logger.warning("OO: brief version record failed (non-fatal): %s", _be)

    # Clean close -> session done -> clear recovery temp.
    if status == 2:
        _clear_recovery(rec_path, rec_manifest)

    return JSONResponse({"error": 0})
