"""
jobs/stage_email_attachments.py

Stage email attachments into the email_attachments ledger (staging + dedupe),
ahead of matching and DMS promotion. For each candidate email:
  - re-fetch raw MIME from EWS by message_id
  - walk attachments (stdlib email; no heavy deps)
  - allowlist: documents always; images only >= IMG_MIN; .ics -> calendar (skip
    DMS); .zip expanded one level and contents allowlisted
  - sha256 each; write blob once per hash under STAGE_ROOT; one email_attachments
    row per (email, hash) occurrence (provenance ledger); idempotent.

Extraction/chunk/embed and DMS promotion are separate, later stages.
Run:  run(tenant_id, only_email='dennis@hjmmlegal.com', limit=None, since_days=60)
"""
from __future__ import annotations
import io, os, sys, email, hashlib, logging, mimetypes, zipfile
from email import policy
sys.path.insert(0, "/app")
from jobs.exchange_sync import _get_db_conn, _get_credentials, _get_config

log = logging.getLogger("stage_email_attachments")

STAGE_ROOT = "/mnt/praesidium/_email_staging"
IMG_MIN = 50_000
MIN_SIZE = 512
ALLOW_EXT = {"pdf","doc","docx","xls","xlsx","csv","ppt","pptx","rtf","txt","eml","msg"}
IMG_EXT   = {"png","jpg","jpeg","gif","tif","tiff","bmp","webp"}
ZIP_EXT   = {"zip"}
ICS_EXT   = {"ics"}
DOC_CT = {
    "application/pdf","application/msword","text/csv","text/plain","application/rtf",
    "text/rtf","message/rfc822","application/vnd.ms-excel","application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}
ZIP_CT = {"application/zip","application/x-zip-compressed","multipart/x-zip"}
SKIP_CT = {"application/pgp-signature","application/pkcs7-signature","application/x-pkcs7-signature"}


def _ext(fn):
    fn = fn or ""
    return fn.rsplit(".", 1)[-1].lower() if "." in fn else ""


def _decide(fn, ct, size):
    ct = (ct or "").lower().split(";")[0].strip()
    ext = _ext(fn)
    if ext in ICS_EXT or ct == "text/calendar":
        return "calendar"
    if ext in ZIP_EXT or ct in ZIP_CT:
        return "zip"
    if ext in ALLOW_EXT or ct in DOC_CT:
        return "keep"
    if ext in IMG_EXT or ct.startswith("image/"):
        return "keep" if size >= IMG_MIN else "skip"
    return "skip"


def _walk_eml(raw):
    out = []
    msg = email.message_from_bytes(raw, policy=policy.default)
    for part in msg.walk():
        if part.is_multipart():
            continue
        ct = (part.get_content_type() or "").lower()
        if ct in SKIP_CT:
            continue
        cd = str(part.get("Content-Disposition") or "")
        fname = part.get_filename()
        if not (fname or "attachment" in cd.lower()):
            continue
        try:
            payload = part.get_content()
        except Exception:
            payload = part.get_payload(decode=True)
        if isinstance(payload, str):
            payload = payload.encode("utf-8", "replace")
        if not isinstance(payload, (bytes, bytearray)) or len(payload) < MIN_SIZE:
            continue
        fn = (fname or "attachment").replace("/", "_").replace("\\", "_").replace("\x00", "")
        out.append({"filename": fn, "content_type": ct, "data": bytes(payload), "disposition": cd})
    return out


def _expand_zip(att):
    try:
        zf = zipfile.ZipFile(io.BytesIO(att["data"]))
    except Exception as e:
        log.warning("bad zip %s: %s", att["filename"], e)
        return
    for info in zf.infolist():
        if info.is_dir() or info.file_size < MIN_SIZE:
            continue
        name = info.filename.split("/")[-1]
        ct = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if _decide(name, ct, info.file_size) != "keep":
            continue
        try:
            data = zf.read(info)
        except Exception:
            continue
        yield {"filename": "%s::%s" % (att["filename"], name), "content_type": ct, "data": data}


def _build_account(creds, config, email_addr):
    from exchangelib import Credentials, Configuration, Account, IMPERSONATION
    from exchangelib.protocol import BaseProtocol, NoVerifyHTTPAdapter
    import urllib3
    urllib3.disable_warnings()
    BaseProtocol.HTTP_ADAPTER_CLS = NoVerifyHTTPAdapter
    domain = creds.get("domain", "")
    user = creds["username"]
    full = ("%s\\%s" % (domain, user)) if domain else user
    cfg = Configuration(service_endpoint=config.get("ews_url", ""),
                        credentials=Credentials(username=full, password=creds["password"]),
                        auth_type="NTLM")
    return Account(primary_smtp_address=email_addr, config=cfg,
                   autodiscover=False, access_type=IMPERSONATION)


def _fetch_mime(account, message_id):
    for folder in (account.inbox, account.sent):
        try:
            items = list(folder.filter(message_id=message_id).only("mime_content")[:1])
        except Exception:
            items = []
        if items and getattr(items[0], "mime_content", None):
            mc = items[0].mime_content
            return mc if isinstance(mc, (bytes, bytearray)) else str(mc).encode("utf-8", "replace")
    return None


def _stage_one(cur, tid, email_id, fn, ct, data):
    h = hashlib.sha256(data).hexdigest()
    cur.execute("SELECT 1 FROM email_attachments WHERE email_id=%s AND file_hash=%s", (email_id, h))
    if cur.fetchone():
        return "dup_occurrence"
    ext = _ext(fn)
    sub = os.path.join(STAGE_ROOT, tid.strip(), h[:2])
    os.makedirs(sub, exist_ok=True)
    path = os.path.join(sub, h + ("." + ext if ext else ""))
    new_blob = not os.path.exists(path)
    if new_blob:
        with open(path, "wb") as f:
            f.write(data)
    cur.execute("""
        INSERT INTO email_attachments
            (id, tenant_id, email_id, filename, content_type, size_bytes,
             file_hash, staging_path, extraction_status, created_at)
        VALUES (gen_random_uuid(), %s, %s, %s, %s, %s, %s, %s, 'staged', now())
    """, (tid, email_id, fn[:1000], (ct or "")[:255], len(data), h, path))
    return "staged" if new_blob else "staged_dedup_blob"


def run(tenant_id, only_email="dennis@hjmmlegal.com", limit=None, since_days=60):
    conn = _get_db_conn()
    creds = _get_credentials(conn, tenant_id)
    config = _get_config(conn, tenant_id)
    cur = conn.cursor()
    cur.execute("SELECT mapped_user_id FROM connector_entity_map "
                "WHERE connector_type='exchange' AND lower(entity_email)=lower(%s) LIMIT 1",
                (only_email,))
    row = cur.fetchone()
    uid = (row["mapped_user_id"] if row else None)
    if not uid:
        conn.close(); raise ValueError("no mapped_user_id for %s" % only_email)

    q = """
        SELECT id, message_id FROM email_routing_queue eq
        WHERE TRIM(tenant_id)=TRIM(%s) AND has_attachments IS TRUE
          AND received_at >= now() - make_interval(days => %s)
          AND attorney_user_id = %s
          AND NOT EXISTS (SELECT 1 FROM email_attachments ea WHERE ea.email_id = eq.id)
        ORDER BY received_at DESC
    """
    params = [tenant_id, since_days, uid]
    if limit:
        q += " LIMIT %s"; params.append(limit)
    cur.execute(q, params)
    emails = cur.fetchall()

    acct = _build_account(creds, config, only_email)
    stats = {"emails": 0, "no_mime": 0, "staged": 0, "dedup_blob": 0, "dup_occ": 0,
             "calendar": 0, "skipped": 0, "zip_expanded": 0, "errors": 0}

    for em in emails:
        eid = em["id"]; mid = em["message_id"]
        try:
            raw = _fetch_mime(acct, mid)
            if not raw:
                stats["no_mime"] += 1; continue
            for att in _walk_eml(raw):
                verdict = _decide(att["filename"], att["content_type"], len(att["data"]))
                if verdict == "calendar":
                    stats["calendar"] += 1; continue
                if verdict == "skip":
                    stats["skipped"] += 1; continue
                items = list(_expand_zip(att)) if verdict == "zip" else [att]
                if verdict == "zip":
                    stats["zip_expanded"] += 1
                for it in items:
                    r = _stage_one(cur, tenant_id, eid, it["filename"], it["content_type"], it["data"])
                    if r == "staged": stats["staged"] += 1
                    elif r == "staged_dedup_blob": stats["dedup_blob"] += 1
                    else: stats["dup_occ"] += 1
            conn.commit()
            stats["emails"] += 1
        except Exception as e:
            conn.rollback(); stats["errors"] += 1
            log.warning("stage email %s failed: %s", eid, e)

    cur.close(); conn.close()
    return stats
